using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using UnityEditor;
using UnityEditor.Animations;
using UnityEditor.AssetImporters;
using UnityEngine;
using UnityEngine.Rendering;

namespace RotoWeave.Editor
{
    [ScriptedImporter(RotoWeaveProductContract.ScriptedImporterVersion, "rotoweave", "aifcharacter")]
    public sealed class RotoWeaveCharacterImporter : ScriptedImporter
    {
        private const long MaximumArchiveBytes = 2L * 1024L * 1024L * 1024L;
        private const long MaximumEntryBytes = 512L * 1024L * 1024L;
        private const int MaximumEntries = 20000;
        private const string ShadowSpriteGuid = "751304c3809d950687c1317798afaf35";
        private const string VisualObjectName = "Visual";
        private const string EmissionObjectName = "Emission";
        private const string ShadowObjectName = "Shadow";
        private const string EmissionAnimationPath = VisualObjectName + "/" + EmissionObjectName;
        private const string BaseShaderName = "Sprites/Default";
        private const string EmissionShaderName = "RotoWeave/Sprites/Additive Emission";

        [SerializeField, Min(0f)] private float pixelsPerUnit;
        [SerializeField] private FilterMode filterMode = FilterMode.Bilinear;

        public override void OnImportAsset(AssetImportContext context)
        {
            try
            {
                ImportCharacter(context);
            }
            catch (Exception exception)
            {
                context.LogImportError("RotoWeave 角色导入失败：" + exception.Message);
                throw;
            }
        }

        private void ImportCharacter(AssetImportContext context)
        {
            var sourceInfo = new FileInfo(context.assetPath);
            if (!sourceInfo.Exists) throw new InvalidDataException("找不到 .rotoweave 文件，请重新复制后再导入。");
            if (sourceInfo.Length <= 0 || sourceInfo.Length > MaximumArchiveBytes)
            {
                throw new InvalidDataException("文件为空或超过 2 GB 安全上限。");
            }

            using (var stream = File.OpenRead(context.assetPath))
            using (var archive = new ZipArchive(stream, ZipArchiveMode.Read, false))
            {
                var entries = ReadSafeEntries(archive);
                var manifestBytes = ReadRequired(entries, "manifest.json", 8 * 1024 * 1024);
                var checksumsBytes = ReadRequired(entries, "checksums.json", 16 * 1024 * 1024);
                var declaredChecksums = ValidateChecksums(entries, checksumsBytes);

                var manifest = JsonUtility.FromJson<RotoWeaveCharacterManifest>(Utf8(manifestBytes));
                ValidateManifest(manifest, context.assetPath);
                ValidateUnityProject();
                var hasEmission = manifest.atlases.emission != null &&
                                  manifest.atlases.emission.Length > 0;
                Sprite shadowSprite = null;
                var shadowEnabled = manifest.character.shadow != null &&
                                    manifest.character.shadow.enabled ||
                                    manifest.animations.Any(animation =>
                                        animation.frames.Any(frame =>
                                            frame.transform != null &&
                                            frame.transform.shadow != null &&
                                            frame.transform.shadow.enabled));
                if (shadowEnabled)
                {
                    var shadowPath = AssetDatabase.GUIDToAssetPath(ShadowSpriteGuid);
                    if (string.IsNullOrWhiteSpace(shadowPath))
                    {
                        throw new InvalidDataException(
                            "缺少 RotoWeave 全局阴影 Sprite，请重新安装 UnityPackage。");
                    }
                    context.DependsOnSourceAsset(shadowPath);
                    shadowSprite = AssetDatabase.LoadAssetAtPath<Sprite>(shadowPath);
                    if (shadowSprite == null)
                    {
                        throw new InvalidDataException(
                            "RotoWeave 全局阴影纹理未按 Sprite 导入，请重新安装 UnityPackage。");
                    }
                }

                var baseAtlasTextures = LoadAtlasLayer(
                    context,
                    entries,
                    declaredChecksums,
                    manifest.atlases.@base,
                    "Base",
                    false,
                    TextureFormat.RGBA32);
                var emissionAtlasTextures = hasEmission
                    ? LoadAtlasLayer(
                        context,
                        entries,
                        declaredChecksums,
                        manifest.atlases.emission,
                        "Emission",
                        true,
                        TextureFormat.RGB24)
                    : new Dictionary<string, Texture2D>(StringComparer.Ordinal);
                var baseMaterial = CreateLayerMaterial(
                    context,
                    BaseShaderName,
                    "RotoWeave Base Premultiplied",
                    "material_base");
                var emissionMaterial = hasEmission
                    ? CreateLayerMaterial(
                        context,
                        EmissionShaderName,
                        "RotoWeave Emission Additive",
                        "material_emission")
                    : null;

                var baseSprites = new Dictionary<string, Sprite>(StringComparer.Ordinal);
                var emissionSprites = new Dictionary<string, Sprite>(StringComparer.Ordinal);
                var resolvedBasePixelsPerUnit = Mathf.Max(
                    1f,
                    pixelsPerUnit > 0f ? pixelsPerUnit : manifest.character.basePixelsPerUnit);
                BuildSprites(
                    context,
                    manifest.sprites,
                    baseAtlasTextures,
                    emissionAtlasTextures,
                    baseSprites,
                    emissionSprites,
                    resolvedBasePixelsPerUnit,
                    hasEmission);
                var clips = new Dictionary<string, AnimationClip>(StringComparer.Ordinal);
                foreach (var animation in manifest.animations)
                {
                    var clip = BuildClip(
                        context,
                        animation,
                        baseSprites,
                        emissionSprites,
                        resolvedBasePixelsPerUnit,
                        shadowEnabled,
                        shadowSprite,
                        hasEmission);
                    clips.Add(animation.id, clip);
                }

                var controller = BuildController(context, manifest, clips);
                var defaultAnimation = manifest.animations.First(
                    item => item.id == manifest.character.defaultAnimationId);
                var root = new GameObject(string.IsNullOrWhiteSpace(manifest.character.name) ? "RotoWeaveCharacter" : manifest.character.name);
                var animator = root.AddComponent<Animator>();
                animator.runtimeAnimatorController = controller;
                var visual = new GameObject(VisualObjectName);
                visual.transform.SetParent(root.transform, false);
                ApplyTransform(
                    visual.transform,
                    defaultAnimation.frames[0].transform,
                    resolvedBasePixelsPerUnit);
                var renderer = visual.AddComponent<SpriteRenderer>();
                renderer.sharedMaterial = baseMaterial;
                renderer.color = FrameColor(defaultAnimation.frames[0].transform);

                if (defaultAnimation.frames.Length > 0 && baseSprites.TryGetValue(defaultAnimation.frames[0].spriteId, out var defaultSprite))
                {
                    renderer.sprite = defaultSprite;
                }
                if (hasEmission)
                {
                    var emissionObject = new GameObject(EmissionObjectName);
                    emissionObject.transform.SetParent(visual.transform, false);
                    emissionObject.transform.localPosition = Vector3.zero;
                    emissionObject.transform.localRotation = Quaternion.identity;
                    emissionObject.transform.localScale = Vector3.one;
                    var emissionRenderer = emissionObject.AddComponent<SpriteRenderer>();
                    emissionRenderer.sharedMaterial = emissionMaterial;
                    emissionRenderer.color = FrameColor(defaultAnimation.frames[0].transform);
                    emissionRenderer.sortingLayerID = renderer.sortingLayerID;
                    emissionRenderer.sortingOrder = renderer.sortingOrder + 1;
                    if (emissionSprites.TryGetValue(
                            defaultAnimation.frames[0].spriteId,
                            out var defaultEmissionSprite))
                    {
                        emissionRenderer.sprite = defaultEmissionSprite;
                    }
                }
                if (shadowEnabled)
                {
                    ConfigureShadow(
                        root,
                        renderer,
                        shadowSprite,
                        defaultAnimation.frames[0],
                        resolvedBasePixelsPerUnit);
                }

                var bindings = new List<RotoWeaveAnimationBinding>(manifest.animations.Length);
                foreach (var animation in manifest.animations)
                {
                    bindings.Add(new RotoWeaveAnimationBinding
                    {
                        displayName = animation.displayName,
                        stableStateName = animation.displayName,
                        fullPathHash = Animator.StringToHash("Base Layer." + animation.displayName)
                    });
                }
                var character = root.AddComponent<RotoWeaveCharacter>();
                character.ConfigureForImporter(
                    animator,
                    defaultAnimation.displayName,
                    new Vector2(
                        manifest.character.designSize.widthWorld,
                        manifest.character.designSize.heightWorld),
                    manifest.character.designSize.pixelsPerUnit,
                    bindings);

                context.AddObjectToAsset("prefab_" + manifest.character.id, root);
                context.SetMainObject(root);
            }
        }

        private static void BuildSprites(
            AssetImportContext context,
            IEnumerable<RotoWeaveSprite> sprites,
            IReadOnlyDictionary<string, Texture2D> baseAtlasTextures,
            IReadOnlyDictionary<string, Texture2D> emissionAtlasTextures,
            IDictionary<string, Sprite> baseSprites,
            IDictionary<string, Sprite> emissionSprites,
            float resolvedPixelsPerUnit,
            bool hasEmission)
        {
            foreach (var sprite in sprites)
            {
                var baseTexture = baseAtlasTextures[sprite.atlasId];
                var rect = new Rect(
                    sprite.rect.x,
                    sprite.rect.y,
                    sprite.rect.width,
                    sprite.rect.height);
                var pivot = new Vector2(sprite.pivot.x, sprite.pivot.y);
                var spritePixelsPerUnit = Mathf.Max(
                    1f,
                    resolvedPixelsPerUnit * sprite.outputScale);
                var baseSprite = Sprite.Create(
                    baseTexture,
                    rect,
                    pivot,
                    spritePixelsPerUnit,
                    4,
                    SpriteMeshType.FullRect);
                baseSprite.name = sprite.id;
                context.AddObjectToAsset(sprite.id, baseSprite);
                baseSprites.Add(sprite.id, baseSprite);
                if (hasEmission)
                {
                    var emissionSprite = Sprite.Create(
                        emissionAtlasTextures[sprite.atlasId],
                        rect,
                        pivot,
                        spritePixelsPerUnit,
                        4,
                        SpriteMeshType.FullRect);
                    emissionSprite.name = sprite.id + "_Emission";
                    context.AddObjectToAsset("emission_" + sprite.id, emissionSprite);
                    emissionSprites.Add(sprite.id, emissionSprite);
                }
            }
        }

        private AnimationClip BuildClip(
            AssetImportContext context,
            RotoWeaveAnimation animation,
            IReadOnlyDictionary<string, Sprite> baseSprites,
            IReadOnlyDictionary<string, Sprite> emissionSprites,
            float resolvedPixelsPerUnit,
            bool shadowEnabled,
            Sprite shadowSprite,
            bool hasEmission)
        {
            var cycleDuration = ResolveCycleDuration(animation);
            var clip = new AnimationClip
            {
                name = animation.displayName,
                frameRate = Mathf.Clamp(animation.frameRate, 1f, 60f)
            };
            var baseKeys = new List<ObjectReferenceKeyframe>(animation.frames.Length + 1);
            var emissionKeys = hasEmission
                ? new List<ObjectReferenceKeyframe>(animation.frames.Length + 1)
                : null;
            var curves = new Dictionary<string, List<Keyframe>>(StringComparer.Ordinal)
            {
                ["position.x"] = new List<Keyframe>(),
                ["position.y"] = new List<Keyframe>(),
                ["scale.x"] = new List<Keyframe>(),
                ["scale.y"] = new List<Keyframe>(),
                ["rotation"] = new List<Keyframe>(),
                ["color.r"] = new List<Keyframe>(),
                ["color.g"] = new List<Keyframe>(),
                ["color.b"] = new List<Keyframe>(),
                ["color.a"] = new List<Keyframe>(),
            };
            var shadowCurves = new Dictionary<string, List<Keyframe>>(StringComparer.Ordinal)
            {
                ["position.x"] = new List<Keyframe>(),
                ["position.y"] = new List<Keyframe>(),
                ["scale.x"] = new List<Keyframe>(),
                ["scale.y"] = new List<Keyframe>(),
                ["color.r"] = new List<Keyframe>(),
                ["color.g"] = new List<Keyframe>(),
                ["color.b"] = new List<Keyframe>(),
                ["color.a"] = new List<Keyframe>(),
            };
            var frameStartTimes = FrameStartTimes(animation);
            for (var frameIndex = 0; frameIndex < animation.frames.Length; frameIndex++)
            {
                var frame = animation.frames[frameIndex];
                var time = frameStartTimes[frameIndex];
                var baseSprite = baseSprites[frame.spriteId];
                baseKeys.Add(new ObjectReferenceKeyframe { time = time, value = baseSprite });
                if (hasEmission)
                {
                    emissionKeys.Add(
                        new ObjectReferenceKeyframe
                        {
                            time = time,
                            value = emissionSprites[frame.spriteId]
                        });
                }
                var transform = frame.transform;
                var color = FrameColor(transform);
                AddKey(curves, "position.x", time, transform.position.x / resolvedPixelsPerUnit);
                AddKey(curves, "position.y", time, transform.position.y / resolvedPixelsPerUnit);
                AddKey(curves, "scale.x", time, transform.scale.x);
                AddKey(curves, "scale.y", time, transform.scale.y);
                AddKey(curves, "rotation", time, transform.rotationDegrees);
                AddKey(curves, "color.r", time, color.r);
                AddKey(curves, "color.g", time, color.g);
                AddKey(curves, "color.b", time, color.b);
                AddKey(curves, "color.a", time, color.a);
                if (shadowEnabled)
                {
                    var shadow = transform.shadow;
                    var geometry = frame.shadow;
                    var resolvedAlpha = geometry.alpha;
                    var shadowColor = ParseColor(shadow.color, resolvedAlpha);
                    AddKey(shadowCurves, "position.x", time, geometry.positionPx.x / resolvedPixelsPerUnit);
                    AddKey(shadowCurves, "position.y", time, geometry.positionPx.y / resolvedPixelsPerUnit);
                    AddKey(shadowCurves, "scale.x", time, geometry.widthPx / resolvedPixelsPerUnit / shadowSprite.bounds.size.x);
                    AddKey(shadowCurves, "scale.y", time, geometry.depthPx / resolvedPixelsPerUnit / shadowSprite.bounds.size.y);
                    AddKey(shadowCurves, "color.r", time, shadowColor.r);
                    AddKey(shadowCurves, "color.g", time, shadowColor.g);
                    AddKey(shadowCurves, "color.b", time, shadowColor.b);
                    AddKey(shadowCurves, "color.a", time, shadowColor.a);
                }
            }

            AddTerminalObjectKey(baseKeys, animation.loop, cycleDuration);
            AnimationUtility.SetObjectReferenceCurve(
                clip,
                EditorCurveBinding.PPtrCurve(VisualObjectName, typeof(SpriteRenderer), "m_Sprite"),
                baseKeys.ToArray());
            if (hasEmission)
            {
                AddTerminalObjectKey(emissionKeys, animation.loop, cycleDuration);
                AnimationUtility.SetObjectReferenceCurve(
                    clip,
                    EditorCurveBinding.PPtrCurve(EmissionAnimationPath, typeof(SpriteRenderer), "m_Sprite"),
                    emissionKeys.ToArray());
            }
            SetSteppedCurve(clip, VisualObjectName, typeof(Transform), "m_LocalPosition.x", curves["position.x"], animation.loop, cycleDuration);
            SetSteppedCurve(clip, VisualObjectName, typeof(Transform), "m_LocalPosition.y", curves["position.y"], animation.loop, cycleDuration);
            SetSteppedCurve(clip, VisualObjectName, typeof(Transform), "m_LocalScale.x", curves["scale.x"], animation.loop, cycleDuration);
            SetSteppedCurve(clip, VisualObjectName, typeof(Transform), "m_LocalScale.y", curves["scale.y"], animation.loop, cycleDuration);
            SetSteppedCurve(clip, VisualObjectName, typeof(Transform), "localEulerAnglesRaw.z", curves["rotation"], animation.loop, cycleDuration);
            SetRendererColorCurves(clip, VisualObjectName, curves, animation.loop, cycleDuration);
            if (hasEmission)
            {
                SetRendererColorCurves(clip, EmissionAnimationPath, curves, animation.loop, cycleDuration);
            }
            if (shadowEnabled)
            {
                var shadowAnimationPath = ShadowObjectName;
                SetSteppedCurve(clip, shadowAnimationPath, typeof(Transform), "m_LocalPosition.x", shadowCurves["position.x"], animation.loop, cycleDuration);
                SetSteppedCurve(clip, shadowAnimationPath, typeof(Transform), "m_LocalPosition.y", shadowCurves["position.y"], animation.loop, cycleDuration);
                SetSteppedCurve(clip, shadowAnimationPath, typeof(Transform), "m_LocalScale.x", shadowCurves["scale.x"], animation.loop, cycleDuration);
                SetSteppedCurve(clip, shadowAnimationPath, typeof(Transform), "m_LocalScale.y", shadowCurves["scale.y"], animation.loop, cycleDuration);
                SetRendererColorCurves(clip, shadowAnimationPath, shadowCurves, animation.loop, cycleDuration);
            }
            var settings = AnimationUtility.GetAnimationClipSettings(clip);
            settings.loopTime = animation.loop;
            AnimationUtility.SetAnimationClipSettings(clip, settings);
            context.AddObjectToAsset("clip_" + animation.id, clip);
            return clip;
        }

        private static void ConfigureShadow(
            GameObject root,
            SpriteRenderer characterRenderer,
            Sprite sprite,
            RotoWeaveFrame frame,
            float resolvedPixelsPerUnit)
        {
            var shadow = frame.transform.shadow;
            var geometry = frame.shadow;
            var shadowObject = new GameObject(ShadowObjectName);
            shadowObject.transform.SetParent(root.transform, false);
            shadowObject.transform.localPosition = new Vector3(
                geometry.positionPx.x / resolvedPixelsPerUnit,
                geometry.positionPx.y / resolvedPixelsPerUnit,
                0f);
            shadowObject.transform.localRotation = Quaternion.identity;
            shadowObject.transform.localScale = new Vector3(
                geometry.widthPx / resolvedPixelsPerUnit / sprite.bounds.size.x,
                geometry.depthPx / resolvedPixelsPerUnit / sprite.bounds.size.y,
                1f);
            var shadowRenderer = shadowObject.AddComponent<SpriteRenderer>();
            shadowRenderer.sprite = sprite;
            var defaultMaterial = AssetDatabase.GetBuiltinExtraResource<Material>(
                "Sprites-Default.mat");
            if (defaultMaterial != null) shadowRenderer.sharedMaterial = defaultMaterial;
            shadowRenderer.color = ParseColor(
                shadow.color,
                geometry.alpha);
            shadowRenderer.sortingLayerID = characterRenderer.sortingLayerID;
            shadowRenderer.sortingOrder = characterRenderer.sortingOrder - 1;
        }

        private static void ApplyTransform(
            Transform target,
            RotoWeaveFrameTransform transform,
            float resolvedPixelsPerUnit)
        {
            target.localPosition = new Vector3(
                transform.position.x / resolvedPixelsPerUnit,
                transform.position.y / resolvedPixelsPerUnit,
                0f);
            target.localRotation = Quaternion.Euler(0f, 0f, transform.rotationDegrees);
            target.localScale = new Vector3(transform.scale.x, transform.scale.y, 1f);
        }

        private static Color ParseColor(string html, float alpha)
        {
            if (!ColorUtility.TryParseHtmlString(html, out var color))
            {
                throw new InvalidDataException("逐帧颜色不是有效的 #RRGGBB。 ");
            }
            color.a = Mathf.Clamp01(alpha);
            return color;
        }

        private static Color FrameColor(RotoWeaveFrameTransform transform)
        {
            return ParseColor(transform.color, transform.opacity);
        }

        private static void AddKey(
            IDictionary<string, List<Keyframe>> curves,
            string name,
            float time,
            float value)
        {
            curves[name].Add(new Keyframe(time, value));
        }

        private static void AddTerminalObjectKey(
            List<ObjectReferenceKeyframe> keys,
            bool loop,
            float cycleDuration)
        {
            var terminalValue = loop ? keys[0].value : keys[keys.Count - 1].value;
            keys.Add(new ObjectReferenceKeyframe { time = cycleDuration, value = terminalValue });
        }

        private static void SetRendererColorCurves(
            AnimationClip clip,
            string path,
            IReadOnlyDictionary<string, List<Keyframe>> curves,
            bool loop,
            float cycleDuration)
        {
            SetSteppedCurve(clip, path, typeof(SpriteRenderer), "m_Color.r", curves["color.r"], loop, cycleDuration);
            SetSteppedCurve(clip, path, typeof(SpriteRenderer), "m_Color.g", curves["color.g"], loop, cycleDuration);
            SetSteppedCurve(clip, path, typeof(SpriteRenderer), "m_Color.b", curves["color.b"], loop, cycleDuration);
            SetSteppedCurve(clip, path, typeof(SpriteRenderer), "m_Color.a", curves["color.a"], loop, cycleDuration);
        }

        private static void SetSteppedCurve(
            AnimationClip clip,
            string path,
            Type type,
            string propertyName,
            List<Keyframe> keys,
            bool loop,
            float cycleDuration)
        {
            var terminalValue = loop ? keys[0].value : keys[keys.Count - 1].value;
            var resolvedKeys = new List<Keyframe>(keys)
            {
                new Keyframe(cycleDuration, terminalValue)
            };
            var curve = new AnimationCurve(resolvedKeys.ToArray());
            for (var index = 0; index < curve.length; index++)
            {
                AnimationUtility.SetKeyLeftTangentMode(curve, index, AnimationUtility.TangentMode.Constant);
                AnimationUtility.SetKeyRightTangentMode(curve, index, AnimationUtility.TangentMode.Constant);
            }
            AnimationUtility.SetEditorCurve(
                clip,
                EditorCurveBinding.FloatCurve(path, type, propertyName),
                curve);
        }

        private static float ResolveCycleDuration(RotoWeaveAnimation animation)
        {
            return animation.durationSeconds;
        }

        private static float[] FrameStartTimes(RotoWeaveAnimation animation)
        {
            var result = new float[animation.frames.Length];
            var elapsed = 0f;
            for (var index = 0; index < animation.frames.Length; index++)
            {
                result[index] = elapsed;
                elapsed += animation.frames[index].durationSeconds;
            }
            return result;
        }

        private static AnimatorController BuildController(
            AssetImportContext context,
            RotoWeaveCharacterManifest manifest,
            IReadOnlyDictionary<string, AnimationClip> clips)
        {
            var controller = new AnimatorController { name = manifest.character.name + " Animator" };
            var stateMachine = new AnimatorStateMachine { name = "Base Layer" };
            controller.layers = new[]
            {
                new AnimatorControllerLayer
                {
                    name = "Base Layer",
                    defaultWeight = 1f,
                    stateMachine = stateMachine
                }
            };
            context.AddObjectToAsset("controller_" + manifest.character.id, controller);
            context.AddObjectToAsset("state_machine_" + manifest.character.id, stateMachine);

            AnimatorState defaultState = null;
            foreach (var animation in manifest.animations)
            {
                var state = stateMachine.AddState(animation.displayName);
                state.motion = clips[animation.id];
                state.writeDefaultValues = true;
                context.AddObjectToAsset("state_" + animation.id, state);
                if (animation.id == manifest.character.defaultAnimationId) defaultState = state;
            }
            if (defaultState == null) throw new InvalidDataException("默认动画不存在，请在 RotoWeave 中重新指定后导出。");
            stateMachine.defaultState = defaultState;
            return controller;
        }

        private static void ValidateUnityProject()
        {
            if (GraphicsSettings.currentRenderPipeline != null)
            {
                throw new InvalidDataException(
                    "RotoWeave 分层角色包仅支持 Built-in Render Pipeline；" +
                    "当前项目启用了 Scriptable Render Pipeline。");
            }
            if (PlayerSettings.colorSpace != ColorSpace.Linear)
            {
                throw new InvalidDataException(
                    "RotoWeave 分层角色包要求 Player Settings > Color Space 为 Linear。");
            }
            if (!PlayerSettings.GetUseDefaultGraphicsAPIs(BuildTarget.WebGL))
            {
                var webGlApis = PlayerSettings.GetGraphicsAPIs(BuildTarget.WebGL);
                if (webGlApis == null || !webGlApis.Contains(GraphicsDeviceType.OpenGLES3))
                {
                    throw new InvalidDataException(
                        "RotoWeave 分层角色包要求 WebGL2；请为 WebGL 启用 OpenGLES3 图形 API。");
                }
            }
        }

        private Dictionary<string, Texture2D> LoadAtlasLayer(
            AssetImportContext context,
            IReadOnlyDictionary<string, ZipArchiveEntry> entries,
            IReadOnlyDictionary<string, string> declaredChecksums,
            IEnumerable<RotoWeaveAtlas> atlases,
            string layerName,
            bool linear,
            TextureFormat textureFormat)
        {
            var textures = new Dictionary<string, Texture2D>(StringComparer.Ordinal);
            foreach (var atlas in atlases)
            {
                if (!declaredChecksums.TryGetValue(atlas.file, out var declaredSha256) ||
                    !string.Equals(declaredSha256, atlas.sha256, StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidDataException(
                        layerName + " 图集清单 SHA-256 与 checksums.json 不一致：" +
                        atlas.file + "。");
                }
                var pngBytes = ReadRequired(entries, atlas.file, MaximumEntryBytes);
                var texture = new Texture2D(2, 2, textureFormat, false, linear)
                {
                    name = layerName + "_Atlas_" + atlas.id,
                    wrapMode = TextureWrapMode.Clamp,
                    filterMode = filterMode,
                    anisoLevel = 0
                };
                if (!ImageConversion.LoadImage(texture, pngBytes, false))
                {
                    UnityEngine.Object.DestroyImmediate(texture);
                    throw new InvalidDataException(
                        layerName + " 图集不是有效 PNG：" + atlas.file + "。");
                }
                if (texture.width != atlas.width || texture.height != atlas.height)
                {
                    UnityEngine.Object.DestroyImmediate(texture);
                    throw new InvalidDataException(
                        layerName + " 图集尺寸与 manifest.json 不一致：" + atlas.file + "。");
                }
                context.AddObjectToAsset(
                    "atlas_" + layerName.ToLowerInvariant() + "_" + atlas.id,
                    texture);
                textures.Add(atlas.id, texture);
            }
            return textures;
        }

        private static Material CreateLayerMaterial(
            AssetImportContext context,
            string shaderName,
            string materialName,
            string assetIdentifier)
        {
            var shader = Shader.Find(shaderName);
            if (shader == null)
            {
                throw new InvalidDataException(
                    "找不到 RotoWeave Shader（" + shaderName +
                    "），请重新安装完整 UnityPackage。");
            }
            var material = new Material(shader) { name = materialName };
            context.AddObjectToAsset(assetIdentifier, material);
            return material;
        }

        private static Dictionary<string, ZipArchiveEntry> ReadSafeEntries(ZipArchive archive)
        {
            if (archive.Entries.Count > MaximumEntries) throw new InvalidDataException("ZIP 条目数量超过安全上限。");
            var result = new Dictionary<string, ZipArchiveEntry>(StringComparer.Ordinal);
            long total = 0;
            foreach (var entry in archive.Entries)
            {
                var path = entry.FullName;
                if (path.EndsWith("/", StringComparison.Ordinal)) continue;
                ValidateArchivePath(path);
                if (entry.Length < 0 || entry.Length > MaximumEntryBytes) throw new InvalidDataException("ZIP 条目尺寸异常：" + path + "。");
                total += entry.Length;
                if (total > MaximumArchiveBytes) throw new InvalidDataException("ZIP 解压后内容超过 2 GB 安全上限。");
                if (result.ContainsKey(path)) throw new InvalidDataException("ZIP 中存在重复路径：" + path + "。");
                result.Add(path, entry);
            }
            return result;
        }

        private static void ValidateArchivePath(string path)
        {
            if (string.IsNullOrWhiteSpace(path) || path.IndexOf('\\') >= 0 || path.StartsWith("/", StringComparison.Ordinal) || Path.IsPathRooted(path))
            {
                throw new InvalidDataException("ZIP 包含不安全路径。");
            }
            var segments = path.Split('/');
            if (segments.Any(segment => segment.Length == 0 || segment == "." || segment == ".."))
            {
                throw new InvalidDataException("ZIP 包含路径穿越条目：" + path + "。");
            }
        }

        private static byte[] ReadRequired(IReadOnlyDictionary<string, ZipArchiveEntry> entries, string path, long maximumBytes)
        {
            ValidateArchivePath(path);
            if (!entries.TryGetValue(path, out var entry)) throw new InvalidDataException("文件包缺少必要文件：" + path + "。");
            if (entry.Length > maximumBytes) throw new InvalidDataException("文件包条目过大：" + path + "。");
            using (var source = entry.Open())
            using (var output = new MemoryStream((int)Math.Min(entry.Length, int.MaxValue)))
            {
                source.CopyTo(output);
                if (output.Length != entry.Length) throw new InvalidDataException("ZIP 条目读取不完整：" + path + "。");
                return output.ToArray();
            }
        }

        private static Dictionary<string, string> ValidateChecksums(
            IReadOnlyDictionary<string, ZipArchiveEntry> entries,
            byte[] checksumBytes)
        {
            var manifest = JsonUtility.FromJson<RotoWeaveChecksums>(Utf8(checksumBytes));
            if (manifest == null || !string.Equals(manifest.algorithm, "SHA-256", StringComparison.OrdinalIgnoreCase) || manifest.files == null)
            {
                throw new InvalidDataException("checksums.json 格式无效。");
            }
            var declared = new HashSet<string>(StringComparer.Ordinal);
            foreach (var file in manifest.files)
            {
                if (file == null || string.IsNullOrWhiteSpace(file.path) || string.IsNullOrWhiteSpace(file.sha256))
                {
                    throw new InvalidDataException("checksums.json 包含空记录。");
                }
                if (file.path == "checksums.json")
                {
                    throw new InvalidDataException("checksums.json 不能声明自身校验和。");
                }
                if (file.sha256.Length != 64 ||
                    file.sha256.Any(character => !Uri.IsHexDigit(character)))
                {
                    throw new InvalidDataException("checksums.json 包含无效 SHA-256：" + file.path + "。");
                }
                if (!declared.Add(file.path)) throw new InvalidDataException("checksums.json 包含重复路径：" + file.path + "。");
                ValidateArchivePath(file.path);
                if (!entries.TryGetValue(file.path, out var entry))
                {
                    throw new InvalidDataException("文件包缺少校验清单声明的文件：" + file.path + "。");
                }
                if (!string.Equals(
                        Sha256(entry, file.path, MaximumEntryBytes),
                        file.sha256,
                        StringComparison.OrdinalIgnoreCase))
                {
                    throw new InvalidDataException("SHA-256 校验失败：" + file.path + "。请重新导出文件。");
                }
            }
            foreach (var entry in entries.Keys)
            {
                if (entry != "checksums.json" && !declared.Contains(entry))
                {
                    throw new InvalidDataException("ZIP 包含未校验文件：" + entry + "。");
                }
            }
            return manifest.files.ToDictionary(
                file => file.path,
                file => file.sha256,
                StringComparer.Ordinal);
        }

        private static bool HasBlend(
            RotoWeaveLayerRenderContract layer,
            string source,
            string destination)
        {
            return layer != null &&
                   layer.blend != null &&
                   string.Equals(layer.blend.source, source, StringComparison.Ordinal) &&
                   string.Equals(
                       layer.blend.destination,
                       destination,
                       StringComparison.Ordinal);
        }

        private static void ValidateTextureDefaults(
            RotoWeaveTextureDefaults defaults,
            string expectedFormat,
            bool expectedSrgb,
            string layerName)
        {
            if (defaults == null ||
                !string.Equals(defaults.format, expectedFormat, StringComparison.Ordinal) ||
                defaults.sRGB != expectedSrgb ||
                !string.Equals(defaults.wrapMode, "Clamp", StringComparison.Ordinal) ||
                !string.Equals(defaults.filterMode, "Bilinear", StringComparison.Ordinal) ||
                defaults.mipmaps ||
                !string.Equals(defaults.compression, "None", StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    layerName +
                    " 纹理设置必须为 " + expectedFormat +
                    "/Clamp/Bilinear/无 Mipmap/无压缩，且 sRGB=" +
                    expectedSrgb.ToString().ToLowerInvariant() + "。");
            }
        }

        private static void ValidateAtlasLayer(
            IReadOnlyCollection<RotoWeaveAtlas> atlases,
            string requiredPrefix,
            string layerName)
        {
            foreach (var atlas in atlases)
            {
                var relativePath = atlas != null && atlas.file != null &&
                                   atlas.file.StartsWith(requiredPrefix, StringComparison.Ordinal)
                    ? atlas.file.Substring(requiredPrefix.Length)
                    : string.Empty;
                if (atlas == null ||
                    string.IsNullOrWhiteSpace(atlas.id) ||
                    string.IsNullOrWhiteSpace(atlas.file) ||
                    string.IsNullOrWhiteSpace(atlas.sha256) ||
                    atlas.sha256.Length != 64 ||
                    atlas.sha256.Any(character => !Uri.IsHexDigit(character)) ||
                    string.IsNullOrWhiteSpace(relativePath) ||
                    relativePath.IndexOf('/') >= 0 ||
                    relativePath.IndexOf('\\') >= 0 ||
                    !relativePath.EndsWith(".png", StringComparison.OrdinalIgnoreCase) ||
                    atlas.width <= 0 || atlas.width > 8192 ||
                    atlas.height <= 0 || atlas.height > 8192)
                {
                    throw new InvalidDataException(
                        layerName + " 图集条目缺少有效 ID、" + requiredPrefix +
                        " 文件、尺寸或 SHA-256。");
                }
            }
            EnsureUnique(atlases.Select(item => item.id), layerName + " 图集 ID");
            EnsureUnique(atlases.Select(item => item.file), layerName + " 图集文件");
        }

        private static void ValidateManifest(RotoWeaveCharacterManifest manifest, string assetPath)
        {
            if (manifest == null) throw new InvalidDataException("manifest.json 无法解析。");
            if (manifest.formatVersion != RotoWeaveProductContract.CharacterPackageFormat)
            {
                throw new InvalidDataException(
                    "不支持的 .rotoweave formatVersion " + manifest.formatVersion +
                    "；Importer v" + RotoWeaveProductContract.ScriptedImporterVersion +
                    " 仅接受 formatVersion " +
                    RotoWeaveProductContract.CharacterPackageFormat +
                    "。旧格式不会自动兼容，请用当前 RotoWeave 重新导出。");
            }
            if (!string.Equals(
                    manifest.packageShape,
                    RotoWeaveProductContract.CharacterPackageShape,
                    StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    "该角色包不是 RotoWeave " +
                    RotoWeaveProductContract.ProductVersion + " 当前分层图集结构（" +
                    RotoWeaveProductContract.CharacterPackageShape +
                    "）。旧 packageShape 明确不受支持，请在当前工具中重新导出。");
            }
            if (!string.Equals(
                    manifest.coordinateContract,
                    RotoWeaveProductContract.CoordinateContract,
                    StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    "角色包坐标契约不是当前 " +
                    RotoWeaveProductContract.CoordinateContract + "。请重新导出。");
            }
            var extension = Path.GetExtension(assetPath).ToLowerInvariant();
            var expectedGenerator = extension == ".rotoweave"
                ? "RotoWeave"
                : extension == ".aifcharacter"
                    ? "AIFrameTools"
                    : string.Empty;
            if (string.IsNullOrEmpty(expectedGenerator) ||
                manifest.generator == null ||
                !string.Equals(manifest.generator.name, expectedGenerator, StringComparison.Ordinal) ||
                !string.Equals(
                    manifest.generator.version,
                    RotoWeaveProductContract.ProductVersion,
                    StringComparison.Ordinal))
            {
                throw new InvalidDataException(
                    extension == ".aifcharacter"
                        ? "旧 .aifcharacter 不是精确的 AIFrameTools 4.0.0 格式 3 契约。"
                        : "角色包生成器版本不是当前 RotoWeave " +
                          RotoWeaveProductContract.ProductVersion + "。请重新导出。");
            }
            if (manifest.character == null || string.IsNullOrWhiteSpace(manifest.character.id)) throw new InvalidDataException("清单缺少角色 ID。");
            if (!IsFinite(manifest.character.pixelsPerUnit) || manifest.character.pixelsPerUnit <= 0f) throw new InvalidDataException("角色 PPU 必须大于 0。");
            if (manifest.character.designSize == null ||
                string.IsNullOrWhiteSpace(manifest.character.designSize.profileId) ||
                (manifest.character.designSize.sourceUnit != "pixels" &&
                 manifest.character.designSize.sourceUnit != "unity") ||
                manifest.character.designSize.widthPixels <= 0 ||
                manifest.character.designSize.heightPixels <= 0 ||
                !IsFinite(manifest.character.designSize.widthWorld) ||
                !IsFinite(manifest.character.designSize.heightWorld) ||
                !IsFinite(manifest.character.designSize.pixelsPerUnit) ||
                manifest.character.designSize.widthWorld <= 0f ||
                manifest.character.designSize.heightWorld <= 0f ||
                Mathf.Abs(
                    manifest.character.designSize.pixelsPerUnit -
                    RotoWeaveProductContract.CanonicalPixelsPerUnit) > 0.0001f ||
                Mathf.Abs(
                    manifest.character.designSize.widthPixels /
                    manifest.character.designSize.pixelsPerUnit -
                    manifest.character.designSize.widthWorld) > 0.001f ||
                Mathf.Abs(
                    manifest.character.designSize.heightPixels /
                    manifest.character.designSize.pixelsPerUnit -
                    manifest.character.designSize.heightWorld) > 0.001f)
            {
                throw new InvalidDataException(
                    "角色尺寸框缺失或像素、Unity 世界单位与固定 PPU 不一致。");
            }
            if (!IsFinite(manifest.character.canonicalPixelsPerUnit) ||
                !IsFinite(manifest.character.basePixelsPerUnit) ||
                !IsFinite(manifest.character.outputScale) ||
                manifest.character.canonicalPixelsPerUnit <= 0f ||
                Mathf.Abs(
                    manifest.character.canonicalPixelsPerUnit -
                    RotoWeaveProductContract.CanonicalPixelsPerUnit) > 0.0001f ||
                Mathf.Abs(
                    manifest.character.basePixelsPerUnit -
                    manifest.character.canonicalPixelsPerUnit) > 0.0001f ||
                manifest.character.outputScale < 0.1f ||
                manifest.character.outputScale > 1f ||
                Mathf.Abs(
                    manifest.character.pixelsPerUnit -
                    manifest.character.basePixelsPerUnit *
                    manifest.character.outputScale) > 0.001f)
            {
                throw new InvalidDataException(
                    "角色 PPU、基础 PPU 与输出比例不满足当前尺寸契约。");
            }
            if (manifest.renderContract == null ||
                !string.Equals(manifest.renderContract.pipeline, "Built-in", StringComparison.Ordinal) ||
                !string.Equals(manifest.renderContract.target, "WebGL2", StringComparison.Ordinal) ||
                !string.Equals(manifest.renderContract.colorSpace, "Linear", StringComparison.Ordinal) ||
                manifest.renderContract.@base == null ||
                !string.Equals(
                    manifest.renderContract.@base.alphaMode,
                    "straight",
                    StringComparison.Ordinal) ||
                !HasBlend(manifest.renderContract.@base, "SrcAlpha", "OneMinusSrcAlpha") ||
                manifest.renderContract.emission == null ||
                !string.Equals(
                    manifest.renderContract.emission.colorSpace,
                    "Linear",
                    StringComparison.Ordinal) ||
                !HasBlend(manifest.renderContract.emission, "One", "One"))
            {
                throw new InvalidDataException(
                    "角色包渲染契约必须是 Built-in/WebGL2/Linear，" +
                    "Base 使用 Straight SrcAlpha/OneMinusSrcAlpha，Emission 使用 One/One。");
            }
            if (manifest.textureDefaults == null)
            {
                throw new InvalidDataException("角色包缺少分层纹理设置。");
            }
            ValidateTextureDefaults(
                manifest.textureDefaults.@base,
                "RGBA32",
                true,
                "Base");
            ValidateTextureDefaults(
                manifest.textureDefaults.emission,
                "RGB24",
                false,
                "Emission");
            if (manifest.atlases == null ||
                manifest.atlases.@base == null ||
                manifest.atlases.@base.Length == 0)
            {
                throw new InvalidDataException("清单没有 Base 图集。");
            }
            if (manifest.sprites == null || manifest.sprites.Length == 0)
                throw new InvalidDataException("清单没有格式 3 顶层 Sprite。");
            if (manifest.animations == null || manifest.animations.Length == 0)
                throw new InvalidDataException("清单没有动画。");

            var baseAtlases = manifest.atlases.@base;
            var emissionAtlases = manifest.atlases.emission ?? Array.Empty<RotoWeaveAtlas>();
            ValidateAtlasLayer(baseAtlases, "atlases/base/", "Base");
            ValidateAtlasLayer(emissionAtlases, "atlases/emission/", "Emission");
            if (emissionAtlases.Length > 0)
            {
                if (emissionAtlases.Length != baseAtlases.Length)
                {
                    throw new InvalidDataException(
                        "Emission 图集必须与 Base 图集逐页配对。");
                }
                var emissionById = emissionAtlases.ToDictionary(
                    item => item.id,
                    StringComparer.Ordinal);
                foreach (var baseAtlas in baseAtlases)
                {
                    if (!emissionById.TryGetValue(baseAtlas.id, out var emissionAtlas) ||
                        emissionAtlas.width != baseAtlas.width ||
                        emissionAtlas.height != baseAtlas.height)
                    {
                        throw new InvalidDataException(
                            "Base 与 Emission 图集的 ID 和尺寸必须逐页一致：" +
                            baseAtlas.id + "。");
                    }
                }
            }
            EnsureUnique(baseAtlases.Select(item => item.id), "Base 图集 ID");
            EnsureUnique(baseAtlases.Select(item => item.file), "Base 图集文件");
            if (emissionAtlases.Length > 0)
            {
                EnsureUnique(emissionAtlases.Select(item => item.id), "Emission 图集 ID");
                EnsureUnique(emissionAtlases.Select(item => item.file), "Emission 图集文件");
            }
            EnsureUnique(manifest.animations.Select(item => item.id), "动画 ID");
            EnsureUnique(manifest.animations.Select(item => item.displayName), "动画显示名称");
            if (!manifest.animations.Any(item => item.id == manifest.character.defaultAnimationId))
            {
                throw new InvalidDataException("默认动画 ID 不在动画列表中。");
            }
            var atlasIds = new HashSet<string>(
                baseAtlases.Select(item => item.id),
                StringComparer.Ordinal);
            var atlasById = baseAtlases.ToDictionary(item => item.id, StringComparer.Ordinal);
            foreach (var sprite in manifest.sprites)
            {
                if (sprite == null ||
                    string.IsNullOrWhiteSpace(sprite.id) ||
                    string.IsNullOrWhiteSpace(sprite.atlasId) ||
                    !atlasIds.Contains(sprite.atlasId) ||
                    sprite.rect == null || sprite.pivot == null ||
                    sprite.rect.x < 0 || sprite.rect.y < 0 ||
                    sprite.rect.width <= 0 || sprite.rect.height <= 0 ||
                    sprite.rect.x + sprite.rect.width > atlasById[sprite.atlasId].width ||
                    sprite.rect.y + sprite.rect.height > atlasById[sprite.atlasId].height ||
                    !IsFinite(sprite.pivot.x) || !IsFinite(sprite.pivot.y) ||
                    sprite.pivot.x < 0f || sprite.pivot.x > 1f ||
                    sprite.pivot.y < 0f || sprite.pivot.y > 1f ||
                    !IsFinite(sprite.outputScale) || sprite.outputScale <= 0f ||
                    sprite.outputScale > 8f || !IsSha256(sprite.sourceSha256))
                {
                    throw new InvalidDataException("格式 3 Sprite 记录无效或越界。");
                }
            }
            EnsureUnique(manifest.sprites.Select(item => item.id), "Sprite ID");
            var spriteIds = new HashSet<string>(
                manifest.sprites.Select(item => item.id),
                StringComparer.Ordinal);
            foreach (var animation in manifest.animations)
            {
                if (animation == null ||
                    string.IsNullOrWhiteSpace(animation.id) ||
                    string.IsNullOrWhiteSpace(animation.displayName))
                {
                    throw new InvalidDataException("动画 ID 或显示名称为空。");
                }
                if (animation.frames == null || animation.frames.Length == 0) throw new InvalidDataException("动画没有帧：" + animation.displayName + "。");
                var duration = ResolveCycleDuration(animation);
                if (!IsFinite(duration) || duration <= 0f || duration > 3600f)
                    throw new InvalidDataException("动画必须声明有效 durationSeconds，且单次播放时长不超过 3600 秒：" + animation.displayName + "。");
                var accumulatedDuration = 0f;
                for (var expectedIndex = 0; expectedIndex < animation.frames.Length; expectedIndex++)
                {
                    var frame = animation.frames[expectedIndex];
                    if (frame == null ||
                        string.IsNullOrWhiteSpace(frame.id) ||
                        string.IsNullOrWhiteSpace(frame.spriteId) ||
                        !spriteIds.Contains(frame.spriteId) ||
                        frame.index != expectedIndex ||
                        !IsFinite(frame.durationSeconds) ||
                        frame.durationSeconds <= 0f ||
                        frame.transform == null ||
                        frame.transform.position == null ||
                        frame.transform.scale == null ||
                        frame.transform.shadow == null ||
                        frame.shadow == null ||
                        frame.transform.shadow.offset == null ||
                        frame.transform.shadow.scale == null ||
                        !IsFinite(frame.transform.position.x) ||
                        !IsFinite(frame.transform.position.y) ||
                        !IsFinite(frame.transform.scale.x) ||
                        !IsFinite(frame.transform.scale.y) ||
                        frame.transform.scale.x <= 0f || frame.transform.scale.y <= 0f ||
                        !IsFinite(frame.transform.rotationDegrees) ||
                        !IsFinite(frame.transform.opacity) ||
                        frame.transform.opacity < 0f || frame.transform.opacity > 1f ||
                        !IsHtmlColor(frame.transform.color) ||
                        !IsHtmlColor(frame.transform.shadow.color) ||
                        !IsFinite(frame.transform.shadow.opacity) ||
                        frame.transform.shadow.opacity < 0f || frame.transform.shadow.opacity > 1f ||
                        !IsFinite(frame.transform.shadow.offset.x) ||
                        !IsFinite(frame.transform.shadow.offset.y) ||
                        !IsFinite(frame.transform.shadow.scale.x) ||
                        !IsFinite(frame.transform.shadow.scale.y) ||
                        frame.transform.shadow.scale.x <= 0f ||
                        frame.transform.shadow.scale.y <= 0f)
                    {
                        throw new InvalidDataException(
                            "动画帧缺少 Sprite、时长或完整变换参数：" +
                            animation.displayName + "。");
                    }
                    accumulatedDuration += frame.durationSeconds;
                }
                foreach (var frame in animation.frames)
                {
                    if (frame.shadow.positionPx == null ||
                        !IsFinite(frame.shadow.positionPx.x) ||
                        !IsFinite(frame.shadow.positionPx.y) ||
                        !IsFinite(frame.shadow.widthPx) || frame.shadow.widthPx <= 0f ||
                        !IsFinite(frame.shadow.depthPx) || frame.shadow.depthPx <= 0f ||
                        !IsFinite(frame.shadow.rotationDegrees) ||
                        !IsFinite(frame.shadow.alpha) || frame.shadow.alpha < 0f || frame.shadow.alpha > 1f ||
                        !IsFinite(frame.shadow.airborneRatio) || frame.shadow.airborneRatio < 0f || frame.shadow.airborneRatio > 1f)
                    {
                        throw new InvalidDataException("动作阴影解析几何无效：" + animation.displayName + "。");
                    }
                }
                if (Mathf.Abs(accumulatedDuration - duration) > 0.0005f)
                    throw new InvalidDataException("动画累计帧时长与 durationSeconds 不一致：" + animation.displayName + "。");
                var resolvedFrameRate = animation.frames.Length / duration;
                if (!IsFinite(animation.frameRate) ||
                    Mathf.Abs(animation.frameRate - resolvedFrameRate) > 0.001f)
                    throw new InvalidDataException("动画 frameRate 与累计帧时长不一致：" + animation.displayName + "。");
            }
        }

        private static bool IsSha256(string value)
        {
            return !string.IsNullOrWhiteSpace(value) &&
                   value.Length == 64 &&
                   value.All(Uri.IsHexDigit);
        }

        private static bool IsHtmlColor(string value)
        {
            return !string.IsNullOrWhiteSpace(value) &&
                   value.Length == 7 &&
                   value[0] == '#' &&
                   value.Skip(1).All(Uri.IsHexDigit);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }

        private static void EnsureUnique(IEnumerable<string> values, string label)
        {
            var unique = new HashSet<string>(StringComparer.Ordinal);
            foreach (var value in values)
            {
                if (string.IsNullOrWhiteSpace(value) || !unique.Add(value)) throw new InvalidDataException(label + " 为空或重复。");
            }
        }

        private static string Utf8(byte[] bytes)
        {
            return System.Text.Encoding.UTF8.GetString(bytes);
        }

        private static string Sha256(
            ZipArchiveEntry entry,
            string path,
            long maximumBytes)
        {
            if (entry.Length < 0 || entry.Length > maximumBytes)
            {
                throw new InvalidDataException("文件包条目过大：" + path + "。");
            }
            using (var source = entry.Open())
            using (var algorithm = SHA256.Create())
            {
                var buffer = new byte[1024 * 1024];
                long total = 0;
                int read;
                while ((read = source.Read(buffer, 0, buffer.Length)) > 0)
                {
                    total += read;
                    if (total > maximumBytes || total > entry.Length)
                    {
                        throw new InvalidDataException(
                            "ZIP 条目展开尺寸异常：" + path + "。");
                    }
                    algorithm.TransformBlock(buffer, 0, read, buffer, 0);
                }
                algorithm.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
                if (total != entry.Length)
                {
                    throw new InvalidDataException(
                        "ZIP 条目读取不完整：" + path + "。");
                }
                return BitConverter.ToString(algorithm.Hash)
                    .Replace("-", string.Empty)
                    .ToLowerInvariant();
            }
        }
    }
}
